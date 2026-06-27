"""HTTP-level tests for the WhatsApp-template CRUD routes (issue #8).

The tests mount the real :class:`FastAPI` app on a vanilla
:class:`TestClient` and exercise ``/v1/templates/*``
end-to-end against an in-memory SQLite database. The point
is to assert the *observable* HTTP contract: status codes,
response shapes and header-driven dependencies – not the
internals of the service layer (covered by
:mod:`tests.services.test_templates`).
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

    Mirrors the pattern used in
    :mod:`tests.routes.test_messages`: the fixture rebuilds
    the cached engine in :mod:`app.db` so the application's
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


def _register_other(client: TestClient) -> dict[str, Any]:
    """Register a second known-good client and return the parsed body."""
    response = client.post(
        "/v1/auth/register",
        json={
            "name": "Other SpA",
            "email": "other@acme.cl",
            "rut": "11.111.111-1",
            "password": "another-secret",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# POST /v1/templates
# ---------------------------------------------------------------------------


def test_create_template_returns_201_with_template(
    client: TestClient,
) -> None:
    """A well-formed request lands in the database and
    returns the persisted row in ``draft`` status."""
    body = _register(client)
    response = client.post(
        "/v1/templates",
        json={
            "name": "order_confirmation",
            "language": "es_CL",
            "category": "utility",
            "components": [
                {"type": "BODY", "text": "Hola {{1}}"},
            ],
            "description": "Confirmación de pedido",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["template"]["status"] == "draft"
    assert payload["template"]["name"] == "order_confirmation"
    assert payload["template"]["language"] == "es_CL"
    assert payload["template"]["category"] == "utility"
    assert payload["template"]["meta_template_id"] is None
    assert payload["template"]["description"] == "Confirmación de pedido"
    assert payload["template"]["components"] == [
        {"type": "BODY", "text": "Hola {{1}}"},
    ]
    # The platform assigns a UUID – the assertion is the
    # length / shape check so a refactor that switches to a
    # different id format cannot break the contract.
    assert len(payload["template"]["id"]) == 36


def test_create_template_defaults_category_to_utility(
    client: TestClient,
) -> None:
    """Omitting ``category`` falls back to ``utility`` – the
    Meta default for transactional templates."""
    body = _register(client)
    response = client.post(
        "/v1/templates",
        json={
            "name": "order_confirmation",
            "language": "es_CL",
            "components": [],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["template"]["category"] == "utility"


def test_create_template_401_when_api_key_missing(
    client: TestClient,
) -> None:
    """A missing ``X-API-Key`` is rejected with the same
    401 contract the other endpoints use, so the
    dashboard can branch on a single error code."""
    response = client.post(
        "/v1/templates",
        json={
            "name": "order_confirmation",
            "language": "es_CL",
        },
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


def test_create_template_422_on_invalid_name(client: TestClient) -> None:
    """A name with uppercase letters / spaces is rejected at
    the service layer (mirrors Meta's own rules) so the
    customer does not have to round-trip through the WABA
    endpoint to learn the format is wrong."""
    body = _register(client)
    response = client.post(
        "/v1/templates",
        json={
            "name": "Order Confirmation",
            "language": "es_CL",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_name"


def test_create_template_422_on_missing_language(client: TestClient) -> None:
    """A missing language is rejected by Pydantic validation
    with a 422 before the service layer is involved."""
    body = _register(client)
    response = client.post(
        "/v1/templates",
        json={
            "name": "order_confirmation",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422


def test_create_template_409_on_duplicate_name_language(
    client: TestClient,
) -> None:
    """A second template with the same ``(name, language)``
    pair is rejected with 409 + a stable code so the
    dashboard can build a single error-handling shape
    around one code."""
    body = _register(client)
    first = client.post(
        "/v1/templates",
        json={
            "name": "order_confirmation",
            "language": "es_CL",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/v1/templates",
        json={
            "name": "order_confirmation",
            "language": "es_CL",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "duplicate_template"


# ---------------------------------------------------------------------------
# GET /v1/templates
# ---------------------------------------------------------------------------


def test_list_templates_returns_empty_for_new_client(
    client: TestClient,
) -> None:
    """A fresh client has no templates – the endpoint
    returns an empty list (not 404) so the dashboard can
    render the "you have no templates" empty state
    without branching on a status code."""
    body = _register(client)
    response = client.get(
        "/v1/templates", headers={"X-API-Key": body["api_key"]}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["count"] == 0
    assert payload["templates"] == []


def test_list_templates_returns_only_owner_templates(
    client: TestClient,
) -> None:
    """The list endpoint only returns the requesting
    customer's templates – the cross-client guard is the
    same as for messages and webhooks."""
    me = _register(client)
    other = _register_other(client)
    client.post(
        "/v1/templates",
        json={"name": "mine", "language": "es_CL"},
        headers={"X-API-Key": me["api_key"]},
    )
    client.post(
        "/v1/templates",
        json={"name": "theirs", "language": "es_CL"},
        headers={"X-API-Key": other["api_key"]},
    )
    response = client.get(
        "/v1/templates", headers={"X-API-Key": me["api_key"]}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["templates"][0]["name"] == "mine"


def test_list_templates_filters_by_status(client: TestClient) -> None:
    """A ``status`` query parameter narrows the list so the
    dashboard's "Pending review" tab works out of the
    box."""
    body = _register(client)
    headers = {"X-API-Key": body["api_key"]}
    client.post(
        "/v1/templates",
        json={"name": "draft_one", "language": "es_CL"},
        headers=headers,
    )
    client.post(
        "/v1/templates",
        json={"name": "draft_two", "language": "en_US"},
        headers=headers,
    )
    response = client.get(
        "/v1/templates?status=draft", headers=headers
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert {t["name"] for t in payload["templates"]} == {"draft_one", "draft_two"}


def test_list_templates_pagination(client: TestClient) -> None:
    """A ``limit`` / ``offset`` pair paginates the result so
    the dashboard can render a scrollable list without
    pulling every row at once."""
    body = _register(client)
    headers = {"X-API-Key": body["api_key"]}
    for index in range(5):
        client.post(
            "/v1/templates",
            json={"name": f"template_{index}", "language": "es_CL"},
            headers=headers,
        )
    response = client.get(
        "/v1/templates?limit=2&offset=1", headers=headers
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    # Newest-first ordering: ``template_4`` is the first
    # row, so ``offset=1`` skips it and returns
    # ``template_3`` and ``template_2``.
    assert [t["name"] for t in payload["templates"]] == [
        "template_3",
        "template_2",
    ]


def test_list_templates_401_when_api_key_missing(
    client: TestClient,
) -> None:
    """A missing ``X-API-Key`` is rejected with 401."""
    response = client.get("/v1/templates")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


# ---------------------------------------------------------------------------
# GET /v1/templates/{id}
# ---------------------------------------------------------------------------


def test_get_template_returns_row(client: TestClient) -> None:
    """A valid ``template_id`` is returned as-is."""
    body = _register(client)
    headers = {"X-API-Key": body["api_key"]}
    created = client.post(
        "/v1/templates",
        json={"name": "order_confirmation", "language": "es_CL"},
        headers=headers,
    )
    assert created.status_code == 201
    template_id = created.json()["template"]["id"]

    response = client.get(f"/v1/templates/{template_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["id"] == template_id


def test_get_template_404_for_unknown_id(client: TestClient) -> None:
    """An unknown id is a 404 with a stable code so the
    dashboard can branch on a single error code."""
    body = _register(client)
    response = client.get(
        "/v1/templates/00000000-0000-0000-0000-000000000000",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "template_not_found"


def test_get_template_404_for_other_clients_template(
    client: TestClient,
) -> None:
    """A template that belongs to a different client is
    reported as ``not_found`` (not ``forbidden``) so the
    existence of another tenant's resource is not
    leaked."""
    me = _register(client)
    other = _register_other(client)
    created = client.post(
        "/v1/templates",
        json={"name": "private", "language": "es_CL"},
        headers={"X-API-Key": other["api_key"]},
    )
    assert created.status_code == 201
    template_id = created.json()["template"]["id"]

    response = client.get(
        f"/v1/templates/{template_id}",
        headers={"X-API-Key": me["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "template_not_found"


# ---------------------------------------------------------------------------
# PUT /v1/templates/{id}
# ---------------------------------------------------------------------------


def test_update_template_patches_draft(client: TestClient) -> None:
    """A successful update modifies the requested fields
    and leaves the rest untouched."""
    body = _register(client)
    headers = {"X-API-Key": body["api_key"]}
    created = client.post(
        "/v1/templates",
        json={
            "name": "order_confirmation",
            "language": "es_CL",
            "description": "old",
        },
        headers=headers,
    )
    assert created.status_code == 201
    template_id = created.json()["template"]["id"]

    response = client.put(
        f"/v1/templates/{template_id}",
        json={"description": "new"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["template"]["description"] == "new"
    # Untouched fields keep their original value.
    assert payload["template"]["name"] == "order_confirmation"
    assert payload["template"]["language"] == "es_CL"


def test_update_template_409_after_submission(client: TestClient) -> None:
    """A template that has been submitted to Meta cannot
    be mutated through the customer-facing endpoint. The
    409 contract is what the dashboard uses to learn the
    rule quickly."""
    body = _register(client)
    headers = {"X-API-Key": body["api_key"]}
    created = client.post(
        "/v1/templates",
        json={"name": "order_confirmation", "language": "es_CL"},
        headers=headers,
    )
    assert created.status_code == 201
    template_id = created.json()["template"]["id"]
    # Bump the row to ``pending`` so the update path
    # activates the immutable guard.
    client.put(
        f"/v1/templates/{template_id}",
        json={"description": "still draft"},
        headers=headers,
    )
    # Force the row out of ``draft`` through a direct DB
    # call (the platform does not expose a "submit" endpoint
    # yet – the WABA integration is a follow-up).
    async def _submit() -> None:
        from sqlalchemy import select

        from app.models.whatsapp_template import (
            WhatsAppTemplate,
            WhatsAppTemplateStatus,
        )

        async with db_module.get_session_factory()() as session:
            row = (
                await session.execute(
                    select(WhatsAppTemplate).where(WhatsAppTemplate.id == template_id)
                )
            ).scalar_one()
            row.status = WhatsAppTemplateStatus.PENDING
            await session.commit()

    asyncio.run(_submit())

    response = client.put(
        f"/v1/templates/{template_id}",
        json={"description": "after submit"},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "template_immutable"


def test_update_template_404_for_unknown_id(client: TestClient) -> None:
    """An unknown id is a 404 – mirrors the GET contract."""
    body = _register(client)
    response = client.put(
        "/v1/templates/00000000-0000-0000-0000-000000000000",
        json={"description": "x"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "template_not_found"


# ---------------------------------------------------------------------------
# DELETE /v1/templates/{id}
# ---------------------------------------------------------------------------


def test_delete_template_removes_row(client: TestClient) -> None:
    """A successful delete removes the row; a subsequent
    GET returns 404."""
    body = _register(client)
    headers = {"X-API-Key": body["api_key"]}
    created = client.post(
        "/v1/templates",
        json={"name": "order_confirmation", "language": "es_CL"},
        headers=headers,
    )
    assert created.status_code == 201
    template_id = created.json()["template"]["id"]

    response = client.delete(f"/v1/templates/{template_id}", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"id": template_id, "deleted": True}

    follow_up = client.get(f"/v1/templates/{template_id}", headers=headers)
    assert follow_up.status_code == 404


def test_delete_template_404_for_foreign_template(
    client: TestClient,
) -> None:
    """A delete that targets another client's template is
    reported as 404 so the existence of another tenant's
    resource is not leaked."""
    me = _register(client)
    other = _register_other(client)
    created = client.post(
        "/v1/templates",
        json={"name": "private", "language": "es_CL"},
        headers={"X-API-Key": other["api_key"]},
    )
    assert created.status_code == 201
    template_id = created.json()["template"]["id"]

    response = client.delete(
        f"/v1/templates/{template_id}",
        headers={"X-API-Key": me["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "template_not_found"

    # Confirm the row is still there for the legitimate owner.
    follow_up = client.get(
        f"/v1/templates/{template_id}",
        headers={"X-API-Key": other["api_key"]},
    )
    assert follow_up.status_code == 200


def test_delete_template_401_when_api_key_missing(
    client: TestClient,
) -> None:
    """A missing ``X-API-Key`` is rejected with 401."""
    response = client.delete(
        "/v1/templates/00000000-0000-0000-0000-000000000000"
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"
