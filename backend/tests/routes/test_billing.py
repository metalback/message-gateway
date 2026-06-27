"""HTTP-level tests for the billing routes.

The tests mount the real :class:`FastAPI` app on a vanilla
``TestClient`` and exercise ``/v1/billing/*`` end-to-end
against an in-memory SQLite database. The Flow adapter is
replaced with an in-memory stub so no network is required.
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
    """``bcrypt_rounds=4`` and a deterministic Flow sandbox base URL."""
    return Settings(
        secret_key="test-secret",
        jwt_secret="test-jwt-secret",
        bcrypt_rounds=4,
        billing_iva_rate=0.19,
        flow_api_key="test-key",
        flow_secret_key="test-secret",
        flow_base_url="https://sandbox.flow.cl/api",
    )


@pytest.fixture
def app_with_db(monkeypatch, fast_settings):  # noqa: ANN001
    """Return a :class:`FastAPI` app whose DB engine points at
    an isolated in-memory SQLite database.

    Mirrors :func:`app.routes.auth`'s fixture so the billing
    tests share the same wiring. The ``get_db``
    dependency is overridden to use the in-memory
    database, and the module-level engine / factory
    caches are reset.
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
def client(app_with_db):  # noqa: ANN001
    return TestClient(app_with_db)


@pytest.fixture
def stubbed_flow(monkeypatch):  # noqa: ANN001
    """Replace the Flow adapter with an in-memory stub.

    The stub is configured by the test through
    ``stubbed_flow.next_order`` /
    ``stubbed_flow.next_status``. The default values
    cover the happy path.
    """
    from app.adapters.flow import FlowOrder, FlowPaymentStatus

    class _StubFlowClient:
        def __init__(self) -> None:
            self.next_order = FlowOrder(
                token="stub-token",
                redirect_url="https://sandbox.flow.cl/pay/stub",
                raw_json='{"token":"stub-token"}',
            )
            self.next_status = FlowPaymentStatus(
                status=2, payment_id="flow-1", raw_json="{}"
            )
            self.create_calls: list[dict] = []
            self.status_calls: list[str] = []

        async def create_order(self, *, commerce_order, subject, amount_clp, email):
            self.create_calls.append(
                {
                    "commerce_order": commerce_order,
                    "subject": subject,
                    "amount_clp": amount_clp,
                    "email": email,
                }
            )
            return self.next_order

        async def get_status(self, *, token):
            self.status_calls.append(token)
            return self.next_status

    stub = _StubFlowClient()
    # The route imports ``FlowClient`` directly, so the
    # patch must be applied to the route's own module
    # (not on ``app.adapters.flow``). Without this the
    # real httpx-based client runs and the test would
    # try to reach Flow's sandbox.
    import app.routes.billing as billing_routes

    monkeypatch.setattr(billing_routes, "FlowClient", lambda *a, **kw: stub)
    return stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_default(client: TestClient) -> dict[str, Any]:
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


def _seed_plans(client: TestClient) -> None:
    """Seed the three MVP plans through a side-channel (the SQLAlchemy engine).

    The route layer never inserts plans; a real deployment
    is seeded by Alembic. The unit tests use the same
    in-memory engine so the SQL inserts are visible to
    the route handlers.
    """

    engine = db_module.get_engine()

    async def _seed() -> None:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "INSERT INTO planes (id, code, name, price_clp, msg_limit, "
                "extra_msg_price, active, sort_order, billing_period, "
                "created_at) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'starter', "
                "'Starter', 19990, 1000, 25, 1, 10, 'monthly', "
                "'2024-01-01 00:00:00'),"
                "('00000000-0000-0000-0000-000000000002', 'growth', "
                "'Growth', 79990, 10000, 18, 1, 20, 'monthly', "
                "'2024-01-01 00:00:00'),"
                "('00000000-0000-0000-0000-000000000003', 'enterprise', "
                "'Enterprise', 0, NULL, NULL, 1, 30, 'monthly', "
                "'2024-01-01 00:00:00')"
            )

    asyncio.run(_seed())


# ---------------------------------------------------------------------------
# GET /v1/billing/plans
# ---------------------------------------------------------------------------


def test_list_plans_returns_active_plans(client: TestClient) -> None:
    """The plans endpoint surfaces the active plans in sort order."""
    _seed_plans(client)
    response = client.get("/v1/billing/plans")
    assert response.status_code == 200
    body = response.json()
    assert [plan["code"] for plan in body] == ["starter", "growth", "enterprise"]
    assert body[0]["price_clp"] == 19990


# ---------------------------------------------------------------------------
# POST /v1/billing/subscriptions
# ---------------------------------------------------------------------------


def test_switch_subscription_updates_plan(client: TestClient) -> None:
    """A successful switch echoes the new plan and the timestamp."""
    _seed_plans(client)
    body = _register_default(client)
    response = client.post(
        "/v1/billing/subscriptions",
        json={"plan_code": "growth"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200, response.text
    new_body = response.json()
    assert new_body["plan"]["code"] == "growth"
    assert new_body["plan"]["price_clp"] == 79990
    assert new_body["switched_at"]


def test_switch_subscription_rejects_unknown_plan(client: TestClient) -> None:
    """A non-existent plan returns 404 with a stable error code."""
    _seed_plans(client)
    body = _register_default(client)
    response = client.post(
        "/v1/billing/subscriptions",
        json={"plan_code": "ghost"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "plan_not_found"


def test_switch_subscription_requires_api_key(client: TestClient) -> None:
    """Missing ``X-API-Key`` header is a 401, not a 403."""
    _seed_plans(client)
    response = client.post(
        "/v1/billing/subscriptions", json={"plan_code": "growth"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


# ---------------------------------------------------------------------------
# GET /v1/billing/balance
# ---------------------------------------------------------------------------


def test_get_balance_returns_current_period_counts(client: TestClient) -> None:
    """The balance endpoint echoes the plan and the usage counters."""
    _seed_plans(client)
    body = _register_default(client)
    response = client.get(
        "/v1/billing/balance", headers={"X-API-Key": body["api_key"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_code"] == "starter"
    assert body["msg_limit"] == 1000
    assert body["used_msgs"] == 0
    assert body["billable_msgs"] == 0
    assert body["overage_msgs"] == 0


# ---------------------------------------------------------------------------
# POST /v1/billing/invoices
# ---------------------------------------------------------------------------


def test_create_invoice_issues_dte_and_returns_payload(
    client: TestClient,
) -> None:
    """A successful invoice creation persists the DTE metadata."""
    _seed_plans(client)
    body = _register_default(client)
    response = client.post(
        "/v1/billing/invoices",
        json={"period": "2026-06-15"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["invoice"]["status"] == "issued"
    assert payload["invoice"]["subtotal_clp"] == 19990
    assert payload["invoice"]["iva_clp"] == 3798
    assert payload["invoice"]["total_clp"] == 23788
    assert payload["dte_number"] > 0
    assert payload["dte_url"]


def test_create_invoice_requires_api_key(client: TestClient) -> None:
    """Missing ``X-API-Key`` is a 401."""
    _seed_plans(client)
    response = client.post("/v1/billing/invoices", json={"period": "2026-06-15"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/billing/invoices
# ---------------------------------------------------------------------------


def test_list_invoices_returns_issued_invoices(client: TestClient) -> None:
    """Issuing an invoice makes it appear in the listing."""
    _seed_plans(client)
    body = _register_default(client)
    # Create two invoices.
    for period in ("2026-05-01", "2026-06-01"):
        response = client.post(
            "/v1/billing/invoices",
            json={"period": period},
            headers={"X-API-Key": body["api_key"]},
        )
        assert response.status_code == 201, response.text

    response = client.get(
        "/v1/billing/invoices", headers={"X-API-Key": body["api_key"]}
    )
    assert response.status_code == 200
    listing = response.json()
    assert len(listing) == 2


def test_get_invoice_returns_single_invoice(client: TestClient) -> None:
    """``GET /v1/billing/invoices/{id}`` echoes the persisted invoice."""
    _seed_plans(client)
    body = _register_default(client)
    created = client.post(
        "/v1/billing/invoices",
        json={"period": "2026-06-15"},
        headers={"X-API-Key": body["api_key"]},
    )
    invoice_id = created.json()["invoice"]["id"]

    response = client.get(
        f"/v1/billing/invoices/{invoice_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    assert response.json()["id"] == invoice_id


def test_get_invoice_returns_404_for_unknown_id(client: TestClient) -> None:
    """An unknown invoice id is a 404 with a stable error code."""
    _seed_plans(client)
    body = _register_default(client)
    response = client.get(
        "/v1/billing/invoices/does-not-exist",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "invoice_not_found"


# ---------------------------------------------------------------------------
# POST /v1/billing/invoices/{id}/pay
# ---------------------------------------------------------------------------


def test_pay_invoice_creates_payment_and_returns_redirect(
    client: TestClient, stubbed_flow
) -> None:
    """A successful payment mints a Flow order and surfaces the redirect URL."""
    _seed_plans(client)
    body = _register_default(client)
    created = client.post(
        "/v1/billing/invoices",
        json={"period": "2026-06-15"},
        headers={"X-API-Key": body["api_key"]},
    )
    invoice_id = created.json()["invoice"]["id"]

    response = client.post(
        f"/v1/billing/invoices/{invoice_id}/pay",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["payment"]["flow_token"] == "stub-token"
    assert payload["redirect_url"] == "https://sandbox.flow.cl/pay/stub"
    assert payload["payment"]["status"] == "pending"
    # The Flow client was actually invoked with the right
    # amount.
    assert stubbed_flow.create_calls and stubbed_flow.create_calls[0]["amount_clp"] == 23788


def test_pay_invoice_404_for_unknown_invoice(client: TestClient, stubbed_flow) -> None:
    """Paying an unknown invoice is a 404."""
    _seed_plans(client)
    body = _register_default(client)
    response = client.post(
        "/v1/billing/invoices/ghost/pay",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /v1/billing/payments
# ---------------------------------------------------------------------------


def test_list_payments_returns_payment_history(
    client: TestClient, stubbed_flow
) -> None:
    """Every successful payment lands in the listing."""
    _seed_plans(client)
    body = _register_default(client)
    created = client.post(
        "/v1/billing/invoices",
        json={"period": "2026-06-15"},
        headers={"X-API-Key": body["api_key"]},
    )
    invoice_id = created.json()["invoice"]["id"]
    client.post(
        f"/v1/billing/invoices/{invoice_id}/pay",
        headers={"X-API-Key": body["api_key"]},
    )

    response = client.get(
        "/v1/billing/payments", headers={"X-API-Key": body["api_key"]}
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_get_payment_refreshes_status_via_flow(
    client: TestClient, stubbed_flow
) -> None:
    """Polling a pending payment calls Flow and updates the local status."""
    from app.adapters.flow import FlowPaymentStatus

    _seed_plans(client)
    body = _register_default(client)
    created = client.post(
        "/v1/billing/invoices",
        json={"period": "2026-06-15"},
        headers={"X-API-Key": body["api_key"]},
    )
    invoice_id = created.json()["invoice"]["id"]
    pay = client.post(
        f"/v1/billing/invoices/{invoice_id}/pay",
        headers={"X-API-Key": body["api_key"]},
    )
    payment_id = pay.json()["payment"]["id"]

    # The stub returns ``status=2`` (paid) on the poll.
    stubbed_flow.next_status = FlowPaymentStatus(
        status=2, payment_id="flow-1", raw_json="{}"
    )

    response = client.get(
        f"/v1/billing/payments/{payment_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert stubbed_flow.status_calls == ["stub-token"]


# ---------------------------------------------------------------------------
# POST /v1/billing/webhook/flow
# ---------------------------------------------------------------------------


def test_flow_webhook_marks_payment_paid(
    client: TestClient, stubbed_flow
) -> None:
    """The webhook flips the payment to ``paid`` on Flow's ``status=2``."""
    _seed_plans(client)
    body = _register_default(client)
    created = client.post(
        "/v1/billing/invoices",
        json={"period": "2026-06-15"},
        headers={"X-API-Key": body["api_key"]},
    )
    invoice_id = created.json()["invoice"]["id"]
    pay = client.post(
        f"/v1/billing/invoices/{invoice_id}/pay",
        headers={"X-API-Key": body["api_key"]},
    )
    flow_token = pay.json()["payment"]["flow_token"]

    response = client.post(
        "/v1/billing/webhook/flow",
        json={"token": flow_token, "status": 2, "payment_id": "flow-abc"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert response.json()["flow_token"] == flow_token


def test_flow_webhook_returns_404_for_unknown_token(client: TestClient) -> None:
    """An unknown token is a 404 with a stable error code."""
    response = client.post(
        "/v1/billing/webhook/flow",
        json={"token": "ghost", "status": 2, "payment_id": "x"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "payment_not_found"


def test_flow_webhook_handles_status_3(
    client: TestClient, stubbed_flow
) -> None:
    """Flow's ``status=3`` (rejected) maps to ``failed``."""
    _seed_plans(client)
    body = _register_default(client)
    created = client.post(
        "/v1/billing/invoices",
        json={"period": "2026-06-15"},
        headers={"X-API-Key": body["api_key"]},
    )
    invoice_id = created.json()["invoice"]["id"]
    pay = client.post(
        f"/v1/billing/invoices/{invoice_id}/pay",
        headers={"X-API-Key": body["api_key"]},
    )
    flow_token = pay.json()["payment"]["flow_token"]

    response = client.post(
        "/v1/billing/webhook/flow",
        json={"token": flow_token, "status": 3},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
