"""Unit tests for the backend subpackage layout.

The scaffold splits the application into ``routes``, ``models``,
``services``, ``adapters`` and ``observability``. These tests pin
the structure so a refactor can't accidentally collapse two
subpackages into one or break the ``app.api`` aggregator.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.api import register_v1
from app.config import Settings
from app.main import create_app

REQUIRED_SUBPACKAGES: tuple[str, ...] = (
    "app.routes",
    "app.models",
    "app.services",
    "app.adapters",
    "app.observability",
)


@pytest.mark.parametrize("subpackage", REQUIRED_SUBPACKAGES)
def test_subpackage_is_importable(subpackage: str) -> None:
    """Every subpackage declared in CODING_STANDARDS.md §2.2 must be
    importable. This catches typos in `__init__.py` and missing
    files that would otherwise surface only at request time."""
    module = importlib.import_module(subpackage)
    assert module.__file__ is not None


# ---------------------------------------------------------------------------
# Top-level infra modules. The scaffold has a small set of
# ``app/<name>.py`` modules that act as singletons (DB engine,
# Redis client) or own cross-cutting configuration (logging).
# They are not subpackages – the list above is for the
# directories under ``app/`` – so they get their own import
# fixture test.
# ---------------------------------------------------------------------------

REQUIRED_INFRA_MODULES: tuple[str, ...] = (
    # PostgreSQL: async engine + session factory.
    "app.db",
    # Redis: cached async client shared by the API and the
    # Arq worker.
    "app.redis_client",
)


@pytest.mark.parametrize("module_name", REQUIRED_INFRA_MODULES)
def test_infra_module_is_importable(module_name: str) -> None:
    """The infra singletons must be importable without side
    effects (no live DB / Redis connection) so a fresh
    ``create_app()`` call does not require the backing
    services to be reachable. CODING_STANDARDS.md §2.5 pins
    the lazy-init contract; this test guards it."""
    module = importlib.import_module(module_name)
    assert module.__file__ is not None


def test_redis_client_exposes_singleton_getter() -> None:
    """``app.redis_client`` must export ``get_redis_client``
    (the cached factory every caller uses) and
    ``reset_redis_client`` (the test-only escape hatch). A
    missing export means the health check cannot resolve
    the client and the import would only fail at request
    time."""
    from app import redis_client

    assert callable(getattr(redis_client, "get_redis_client", None))
    assert callable(getattr(redis_client, "reset_redis_client", None))


def test_observability_logging_module_is_present() -> None:
    """``app.observability.logging`` is the centralised
    logging configuration entry point. Its presence is
    pinned here so a refactor that moves the helpers does
    not silently break the ``configure_logging`` call in
    :func:`app.main.create_app`."""
    from app.observability import logging as logging_module

    assert logging_module.__file__ is not None
    assert callable(getattr(logging_module, "configure_logging", None))
    assert callable(getattr(logging_module, "get_logger", None))


def test_observability_package_exports_logging_helpers() -> None:
    """The two logging helpers must be re-exported from
    ``app.observability`` so call sites can import them from
    a single place. The contract is verified here rather
    than in the per-module tests so a refactor that splits
    the helpers across two modules does not silently drop
    them from the public surface."""
    from app import observability

    assert callable(getattr(observability, "configure_logging", None))
    assert callable(getattr(observability, "get_logger", None))


def test_routes_expose_api_routers() -> None:
    """Each route module is expected to expose a module-level
    ``router`` of type ``APIRouter``. The aggregator iterates over
    them; missing or mis-typed routers would break ``register_v1``."""
    from app.routes import auth, billing, messages, templates, webhooks

    for module in (auth, billing, messages, templates, webhooks):
        assert isinstance(
            module.router, APIRouter
        ), f"{module.__name__} must expose an APIRouter named 'router'"


def test_register_v1_returns_api_router() -> None:
    """The aggregator must return an ``APIRouter`` so the
    application factory can ``include_router`` it once."""
    v1 = register_v1()
    assert isinstance(v1, APIRouter)
    # The prefix pins the public surface; changing it is a breaking change.
    assert v1.prefix == "/v1"


def test_register_v1_aggregates_every_feature_router() -> None:
    """Every feature router from :mod:`app.routes` should be
    reachable through the aggregator. We check the OpenAPI schema
    because the nested-routes wrapper in FastAPI doesn't expose
    its children's prefixes directly through ``router.routes``."""
    app = FastAPI()
    app.include_router(register_v1())
    schema = app.openapi()
    paths = set(schema.get("paths", {}).keys())
    expected_prefixes = (
        "/v1/messages",
        "/v1/templates",
        "/v1/webhooks",
        "/v1/auth",
    )
    for prefix in expected_prefixes:
        assert any(
            p.startswith(prefix) for p in paths
        ), f"Expected a route under {prefix!r}; got {sorted(paths)}"


def test_create_app_mounts_v1_api() -> None:
    """``create_app`` mounts ``/v1`` and the discovery endpoint
    responds, proving the aggregator is wired into the factory."""
    app = create_app(Settings())
    client = TestClient(app)

    response = client.get("/v1")
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "v1"
    assert isinstance(body["routes"], list)


def test_v1_discovery_lists_feature_prefixes() -> None:
    """The discovery endpoint must surface every feature prefix
    that ``app.api.register_v1`` aggregates, so an operator can
    confirm the wiring without opening the OpenAPI docs."""
    app = create_app(Settings())
    client = TestClient(app)

    response = client.get("/v1")
    assert response.status_code == 200
    body = response.json()
    expected = {"/v1/messages", "/v1/templates", "/v1/webhooks", "/v1/auth", "/v1/billing"}
    assert expected.issubset(set(body["routes"])), body["routes"]


def test_placeholder_routes_return_not_implemented() -> None:
    """Every placeholder endpoint in :mod:`app.routes` returns
    ``501 Not Implemented`` so the contract is explicit: the path
    exists, the feature doesn't yet. The auth ``/register`` and
    ``/login`` endpoints are no longer placeholders (issue #3),
    the message-sending endpoints are no longer placeholders
    (issue #4), the billing endpoints are no longer placeholders
    (issue #7), and the WhatsApp-template endpoints are no
    longer placeholders (issue #8), so they are excluded from
    this list."""
    app = create_app(Settings())
    client = TestClient(app)

    cases = (
        ("GET", "/v1/webhooks", 501),
    )
    for method, path, expected_status in cases:
        response = client.request(method, path)
        assert (
            response.status_code == expected_status
        ), f"{method} {path} returned {response.status_code}, expected {expected_status}"


def test_auth_register_endpoint_is_implemented() -> None:
    """``POST /v1/auth/register`` was a placeholder until issue #3
    landed; the test guards against a regression that would
    re-introduce the 501 behaviour now that the endpoint is
    real."""
    app = create_app(Settings())
    client = TestClient(app)

    # An empty body fails Pydantic validation with 422 (not 501);
    # the assertion is the *not-501* half of the contract: as
    # long as the endpoint behaves like a real handler, the
    # placeholder contract is dead.
    response = client.post("/v1/auth/register", json={})
    assert response.status_code != 501, (
        "POST /v1/auth/register must be implemented (issue #3), "
        "not the scaffold 501 placeholder"
    )


def test_messages_send_endpoint_is_implemented() -> None:
    """``POST /v1/messages`` was a placeholder until issue #4
    landed; the test guards against a regression that would
    re-introduce the 501 behaviour now that the endpoint is
    real.

    The request is intentionally missing a valid API key, so
    the dependency short-circuits with a 401 (not a 501). The
    assertion is the *not-501* half of the contract: as long
    as the endpoint behaves like a real handler (it consults
    the API-key dependency instead of stubbing the
    response), the placeholder contract is dead.
    """
    app = create_app(Settings())
    client = TestClient(app)

    response = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56912345678", "body": "hola"},
    )
    assert response.status_code != 501, (
        "POST /v1/messages must be implemented (issue #4), "
        "not the scaffold 501 placeholder"
    )


def test_templates_endpoints_are_implemented() -> None:
    """The ``/v1/templates`` endpoints were 501 placeholders
    until issue #8 landed; the test guards against a
    regression that would re-introduce the 501 behaviour
    now that the routes are real.

    The request is intentionally missing a valid API key, so
    the dependency short-circuits with a 401 (not a 501).
    The assertion is the *not-501* half of the contract:
    as long as the endpoint behaves like a real handler
    (it consults the API-key dependency instead of
    stubbing the response), the placeholder contract is
    dead. We exercise ``POST`` (create) and ``GET`` (list)
    so a regression that only flipped one of them would
    still surface.
    """
    app = create_app(Settings())
    client = TestClient(app)

    create = client.post(
        "/v1/templates",
        json={"name": "order_confirmation", "language": "es_CL"},
    )
    assert create.status_code != 501, (
        "POST /v1/templates must be implemented (issue #8), "
        "not the scaffold 501 placeholder"
    )
    listing = client.get("/v1/templates")
    assert listing.status_code != 501, (
        "GET /v1/templates must be implemented (issue #8), "
        "not the scaffold 501 placeholder"
    )
