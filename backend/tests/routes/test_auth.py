"""HTTP-level tests for the auth routes.

The tests mount the real :class:`FastAPI` app on a vanilla
``TestClient`` and exercise ``/v1/auth/*`` end-to-end against
an in-memory SQLite database. The point is to assert the
*observable* HTTP contract: status codes, response shapes and
header-driven dependencies – not the internals of the
service layer (covered by :mod:`tests.services.test_auth`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_settings() -> Settings:
    """``bcrypt_rounds=4`` keeps the suite under a second; the
    auth behaviour does not depend on the cost factor."""
    return Settings(
        secret_key="test-secret",
        jwt_secret="test-jwt-secret",
        bcrypt_rounds=4,
        jwt_ttl_minutes=15,
        api_key_prefix="mgw_live_",
    )


@pytest.fixture
def app_with_db(monkeypatch, fast_settings):  # noqa: ANN001
    """Return a :class:`FastAPI` app whose DB engine points at
    an isolated in-memory SQLite database.

    The fixture rebuilds the cached engine in :mod:`app.db` so
    the application's ``get_db`` dependency opens a fresh
    database for every test. Without the rebuild the cached
    engine would keep pointing at a non-existent PostgreSQL
    host and the very first request would crash.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db as db_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Wipe + recreate the schema through the real engine, so
    # the SQLAlchemy ``Base.metadata`` matches what production
    # migrations would produce.
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

    # ``import app.db as db_module`` binds ``app`` as a module
    # in this scope, so we declare the local explicitly to make
    # the intent (and the type) obvious to mypy.
    test_app = create_app(fast_settings)
    test_app.dependency_overrides[db_module.get_db] = _override_session

    # Also drop the module-level caches so the *real* ``get_db``
    # (used by anything that imports it directly) is bypassed.
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
def real_client(client):  # noqa: ANN001
    """Alias for :func:`client` used by tests that also need
    a separate ``TestClient`` for a different (sub-)app.
    The alias exists so the caller can be explicit about
    "I need the real auth app here" vs. "I need a test
    app of my own making"."""
    return client


# ---------------------------------------------------------------------------
# POST /v1/auth/register
# ---------------------------------------------------------------------------


def test_register_returns_201_and_plain_api_key(client) -> None:
    """A well-formed registration lands in the database and
    returns the plain API key exactly once."""
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
    body = response.json()

    assert body["email"] == "ops@acme.cl"
    assert body["rut"] == "12345678-5"
    assert body["status"] == "active"
    assert body["plan"] == "starter"
    assert body["api_key"].startswith("mgw_live_")
    assert body["api_key_last4"] == body["api_key"][-4:]
    # The bcrypt digest is never part of the response.
    assert "api_key_hash" not in body
    assert "password_hash" not in body


def test_register_409_on_duplicate_email(client) -> None:
    """The second registration with the same email returns a
    409 with a stable error code."""
    payload = {
        "name": "Acme SpA",
        "email": "ops@acme.cl",
        "rut": "12.345.678-5",
        "password": "sup3r-secret",
    }
    first = client.post("/v1/auth/register", json=payload)
    assert first.status_code == 201, first.text

    payload["rut"] = "11.111.111-1"  # different RUT to isolate the email constraint
    second = client.post("/v1/auth/register", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "duplicate_identity"


def test_register_422_on_bad_email(client) -> None:
    """Pydantic rejects a malformed email before the handler
    runs; the response is a vanilla 422 with the validation
    error location."""
    response = client.post(
        "/v1/auth/register",
        json={
            "name": "Acme SpA",
            "email": "not-an-email",
            "rut": "12.345.678-5",
            "password": "sup3r-secret",
        },
    )
    assert response.status_code == 422
    # Pydantic surfaces the failing field by its JSON path.
    detail = response.json()["detail"]
    assert any("email" in str(item.get("loc", [])) for item in detail)


def test_register_422_on_invalid_rut(client) -> None:
    """A RUT with the wrong check digit is rejected by the
    service layer (422 with a stable error code)."""
    response = client.post(
        "/v1/auth/register",
        json={
            "name": "Acme SpA",
            "email": "ops@acme.cl",
            "rut": "12.345.678-0",
            "password": "sup3r-secret",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_rut"


def test_register_422_on_weak_password(client) -> None:
    """A password shorter than the minimum length is
    rejected at the service layer with a stable code."""
    response = client.post(
        "/v1/auth/register",
        json={
            "name": "Acme SpA",
            "email": "ops@acme.cl",
            "rut": "12.345.678-5",
            "password": "short",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "weak_password"


# ---------------------------------------------------------------------------
# POST /v1/auth/login
# ---------------------------------------------------------------------------


def _register_default(client) -> dict:
    """Helper: register a known-good client and return the parsed body."""
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


def test_login_returns_jwt_for_valid_credentials(client) -> None:
    """The login response carries a bearer token and the
    client summary."""
    _register_default(client)
    response = client.post(
        "/v1/auth/login",
        json={"email": "ops@acme.cl", "password": "sup3r-secret"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["token"]
    assert body["client"]["email"] == "ops@acme.cl"
    assert body["client"]["status"] == "active"


def test_login_401_for_wrong_password(client) -> None:
    """A wrong password returns 401 with the generic
    ``invalid_credentials`` code so account enumeration is
    not feasible."""
    _register_default(client)
    response = client.post(
        "/v1/auth/login",
        json={"email": "ops@acme.cl", "password": "not-it"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_credentials"


def test_login_401_for_unknown_email(client) -> None:
    """Unknown email and wrong password must surface the
    same error code."""
    response = client.post(
        "/v1/auth/login",
        json={"email": "ghost@acme.cl", "password": "whatever"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_credentials"


# ---------------------------------------------------------------------------
# GET /v1/auth/me
# ---------------------------------------------------------------------------


def test_me_returns_client_for_valid_api_key(client) -> None:
    """``GET /v1/auth/me`` echoes the client identified by
    the ``X-API-Key`` header."""
    body = _register_default(client)
    response = client.get("/v1/auth/me", headers={"X-API-Key": body["api_key"]})
    assert response.status_code == 200
    assert response.json()["email"] == "ops@acme.cl"


def test_me_401_when_header_missing(client) -> None:
    """A missing ``X-API-Key`` is a 401 with a stable code."""
    response = client.get("/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


def test_me_401_for_invalid_api_key(client) -> None:
    """A bogus key is rejected without revealing whether the
    key was well-formed but unknown vs. simply absent."""
    response = client.get("/v1/auth/me", headers={"X-API-Key": "mgw_live_bogus"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_api_key"


# ---------------------------------------------------------------------------
# POST /v1/auth/api-keys/rotate
# ---------------------------------------------------------------------------


def test_rotate_returns_new_key_and_invalidates_old(client) -> None:
    """Rotating produces a fresh key; the old one stops
    working for subsequent requests."""
    body = _register_default(client)
    old_key = body["api_key"]

    response = client.post("/v1/auth/api-keys/rotate", headers={"X-API-Key": old_key})
    assert response.status_code == 200, response.text
    rotated = response.json()
    assert rotated["api_key"] != old_key
    assert rotated["api_key_last4"] == rotated["api_key"][-4:]

    # The new key works.
    me = client.get("/v1/auth/me", headers={"X-API-Key": rotated["api_key"]})
    assert me.status_code == 200

    # The old key is now a 401.
    stale = client.get("/v1/auth/me", headers={"X-API-Key": old_key})
    assert stale.status_code == 401


def test_rotate_requires_api_key(client) -> None:
    """Rotating without a key is a 401, not a 403 – the
    caller has not authenticated at all."""
    response = client.post("/v1/auth/api-keys/rotate")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


def test_rotate_401_when_api_key_invalid(client) -> None:
    """A bogus key is rejected with the same 401 contract as
    ``/me``; the test guards against a regression that would
    leak the difference between "missing" and "invalid"
    keys to the client."""
    response = client.post(
        "/v1/auth/api-keys/rotate",
        headers={"X-API-Key": "mgw_live_does-not-exist"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_api_key"


# ---------------------------------------------------------------------------
# require_session
#
# ``require_session`` is a FastAPI dependency that decodes a
# ``Bearer`` JWT. It is not wired to a route in this PR – the
# next auth-related task (the dashboard endpoints) will pick
# it up – so the tests below exercise the dependency directly
# through a tiny ad-hoc app. Pinning the contract here means
# a future caller can rely on the same 401 / decode semantics
# without having to re-derive them.
# ---------------------------------------------------------------------------


def _build_session_app():
    """Build a FastAPI app with a single route guarded by
    :func:`app.routes.auth.require_session`. Kept tiny so the
    test stays focused on the dependency contract."""
    from fastapi import Depends, FastAPI

    from app.routes.auth import require_session

    app = FastAPI()

    @app.get("/_test_session")
    async def _endpoint(payload: dict = Depends(require_session)) -> dict:
        return {"sub": payload.get("sub"), "email": payload.get("email")}

    return app


def test_require_session_rejects_missing_header() -> None:
    """No ``Authorization`` header at all is a 401 with a
    stable error code – not a 403, since the caller never
    authenticated."""
    client = TestClient(_build_session_app())
    response = client.get("/_test_session")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_token"


def test_require_session_rejects_non_bearer_header() -> None:
    """An ``Authorization`` value that does not use the
    ``Bearer`` scheme is a 401 too – the dashboard must use
    the standard scheme to play nicely with HTTP clients that
    auto-attach ``Basic`` creds."""
    client = TestClient(_build_session_app())
    response = client.get("/_test_session", headers={"Authorization": "Basic foo"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_token"


def test_require_session_accepts_valid_token(real_client) -> None:
    """A round-trip: log in to get a token, then use it to
    access a guarded endpoint."""
    # Register + log in to obtain a real token.
    _register_default(real_client)
    login = real_client.post(
        "/v1/auth/login",
        json={"email": "ops@acme.cl", "password": "sup3r-secret"},
    )
    assert login.status_code == 200
    token = login.json()["token"]

    # Hit the session-guarded endpoint with the token.
    session_client = TestClient(_build_session_app())
    response = session_client.get("/_test_session", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "ops@acme.cl"
    assert body["sub"]  # client id is a non-empty UUID


def test_require_session_rejects_garbage_token() -> None:
    """A bearer-prefixed but otherwise invalid token is
    rejected with the generic ``invalid_token`` code."""
    client = TestClient(_build_session_app())
    response = client.get("/_test_session", headers={"Authorization": "Bearer not.a.jwt"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_token"
