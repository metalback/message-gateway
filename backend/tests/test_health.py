"""Smoke tests for the FastAPI app factory and the `/health` endpoint.

These are the only tests the scaffold ships with; they exist to make
sure the application boots, the OpenAPI schema is generated, and the
meta endpoints respond as documented. Domain tests will be added in
later tasks.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import APP_NAME, APP_VERSION, create_app


@pytest.fixture
def client() -> TestClient:
    """A TestClient bound to a freshly-constructed app.

    Building a new app per test (rather than reusing the module-level
    singleton) keeps each test isolated from environment mutations.
    """
    app = create_app(Settings())
    return TestClient(app)


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == APP_NAME
    assert body["version"] == APP_VERSION


def test_root_redirects_to_docs(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert "docs" in body
    assert body["docs"] == "/docs"


def test_openapi_schema_is_served(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == APP_NAME
    assert "/health" in schema["paths"]
    # The readiness endpoint is part of the public meta surface and
    # must be discoverable via the generated OpenAPI document.
    assert "/health/ready" in schema["paths"]


def test_docs_endpoint_is_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_cors_middleware_allows_configured_origin() -> None:
    settings = Settings(cors_allow_origins="http://example.com")
    app = create_app(settings)
    client = TestClient(app)

    response = client.get(
        "/health",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "http://example.com"


def test_create_app_uses_cached_settings_by_default() -> None:
    # Two builds without arguments should produce distinct objects
    # (no shared mutable state) but both succeed.
    a = create_app()
    b = create_app()
    assert a is not b


def test_health_ready_returns_200_when_all_checks_pass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readiness probe must return 200 + ``status="ok"`` when
    every registered dependency probe succeeds."""
    from app import health as health_module
    from app import main as main_module

    ok_check = health_module.HealthStatus(name="database", ok=True)
    ok_redis = health_module.HealthStatus(name="redis", ok=True)

    async def _fake_checks(_settings: Any) -> list[health_module.HealthStatus]:
        return [ok_check, ok_redis]

    # ``app.main`` imports ``run_readiness_checks`` at module load
    # time, so we have to patch the reference the route actually
    # resolves, not the one in ``app.health``.
    monkeypatch.setattr(main_module, "run_readiness_checks", _fake_checks)

    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == APP_NAME
    assert body["version"] == APP_VERSION
    assert body["checks"] == [ok_check.to_dict(), ok_redis.to_dict()]


def test_health_ready_returns_503_when_a_check_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing dependency probe must surface as a 503 + ``status=
    "degraded"`` so a load-balancer can take the pod out of rotation
    until the dependency recovers."""
    from app import health as health_module
    from app import main as main_module

    bad_check = health_module.HealthStatus(name="database", ok=False, detail="connection refused")
    ok_check = health_module.HealthStatus(name="redis", ok=True)

    async def _fake_checks(_settings: Any) -> list[health_module.HealthStatus]:
        return [bad_check, ok_check]

    monkeypatch.setattr(main_module, "run_readiness_checks", _fake_checks)

    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"][0]["ok"] is False
    assert body["checks"][0]["detail"] == "connection refused"
    assert body["checks"][1]["ok"] is True


def test_health_ready_lists_check_results_in_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response body must always include a ``checks`` list with
    one entry per registered probe. This guarantees the contract an
    operator can rely on when triaging an outage."""
    from app import health as health_module
    from app import main as main_module

    async def _fake_checks(_settings: Any) -> list[health_module.HealthStatus]:
        return [
            health_module.HealthStatus(name="database", ok=True),
            health_module.HealthStatus(name="redis", ok=True),
        ]

    monkeypatch.setattr(main_module, "run_readiness_checks", _fake_checks)

    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["checks"], list)
    assert {check["name"] for check in body["checks"]} == {"database", "redis"}
